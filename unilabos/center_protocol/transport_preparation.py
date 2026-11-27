"""独立车辆准备信封；与执行结果共用序号而不伪造本地 Task 身份。"""

from typing import Any
from uuid import UUID

from .digest import digest, is_digest
from .transport_endpoint import validate_endpoint


def uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).int == 0:
        raise ValueError("搬运准备身份必须为非空规范 UUID")


def validate_request(value: Any) -> dict:
    fields = {
        "schema_version",
        "request_uuid",
        "generation",
        "global_task_uuid",
        "station_invocation_uuid",
        "handoff_uuid",
        "logistics_station_uuid",
        "contract_uuid",
        "contract_digest",
        "input_digest",
        "condition_digest",
        "vehicle_selection",
        "source_endpoint",
        "target_endpoint",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("搬运准备请求字段或版本无效")
    for key in [
        "request_uuid",
        "global_task_uuid",
        "station_invocation_uuid",
        "handoff_uuid",
        "logistics_station_uuid",
        "contract_uuid",
    ]:
        uuid(value[key])
    if type(value["generation"]) is not int or not 1 <= value["generation"] <= 2**53 - 1:
        raise ValueError("搬运准备代次无效")
    if not all(is_digest(value[k]) for k in ("contract_digest", "input_digest", "condition_digest")):
        raise ValueError("搬运准备摘要无效")
    for key in ("source_endpoint", "target_endpoint"):
        validate_endpoint(value[key])
    source, target = value["source_endpoint"], value["target_endpoint"]
    if source["station_uuid"] == target["station_uuid"] or {i["container_uuid"] for i in source["items"]} != {
        i["container_uuid"] for i in target["items"]
    }:
        raise ValueError("取送两端必须覆盖同一批容器且属于不同工站")
    selection = value["vehicle_selection"]
    if selection != {"kind": "auto"}:
        if (
            not isinstance(selection, dict)
            or set(selection) != {"kind", "vehicle_id"}
            or selection["kind"] != "fixed"
            or not isinstance(selection["vehicle_id"], str)
            or not 1 <= len(selection["vehicle_id"]) <= 200
            or any(not (c.isascii() and (c.isalnum() or c in "_.-")) for c in selection["vehicle_id"])
        ):
            raise ValueError("搬运准备选车策略无效")
    return value


def validate_binding(value: Any) -> dict:
    fields = {
        "schema_version",
        "request_uuid",
        "generation",
        "allocation_uuid",
        "vehicle_resource_uuid",
        "vehicle_id",
        "allocation_fence",
        "executor_bindings",
        "resource_keys",
        "endpoint_snapshot_digest",
        "route_binding",
        "route_binding_digest",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema_version"] != 1
        or type(value["schema_version"]) is not int
    ):
        raise ValueError("搬运执行绑定字段无效")
    route = value["route_binding"]
    if (
        not is_digest(value["endpoint_snapshot_digest"])
        or not is_digest(value["route_binding_digest"])
        or not isinstance(route, dict)
        or set(route) != {"schema_version", "profile_uuid", "profile_digest", "source", "target"}
        or type(route["schema_version"]) is not int
        or route["schema_version"] != 1
        or not is_digest(route["profile_digest"])
        or digest(route) != value["route_binding_digest"]
    ):
        raise ValueError("接驳绑定摘要或版本无效")
    uuid(route["profile_uuid"])
    for side in ("source", "target"):
        points = route[side]
        if not isinstance(points, list) or not 1 <= len(points) <= 100:
            raise ValueError("取送接驳映射数量无效")
        seen = set()
        for point in points:
            if (
                not isinstance(point, dict)
                or set(point) != {"container_uuid", "site_uuid", "nav_point_ref", "slot_ref"}
                or any(
                    not isinstance(point[k], str) or not point[k].strip() or len(point[k]) > 200
                    for k in ("nav_point_ref", "slot_ref")
                )
            ):
                raise ValueError("取送接驳映射字段无效")
            uuid(point["container_uuid"])
            uuid(point["site_uuid"])
            if point["container_uuid"] in seen:
                raise ValueError("接驳映射容器重复")
            seen.add(point["container_uuid"])
    for key in ("request_uuid", "allocation_uuid", "vehicle_resource_uuid", "allocation_fence"):
        uuid(value[key])
    if (
        type(value["generation"]) is not int
        or not 1 <= value["generation"] <= 2**53 - 1
        or not isinstance(value["vehicle_id"], str)
        or not 1 <= len(value["vehicle_id"]) <= 200
        or any(not (c.isascii() and (c.isalnum() or c in "_.-")) for c in value["vehicle_id"])
        or value["executor_bindings"] != {"transport_vehicle": value["vehicle_resource_uuid"]}
        or not isinstance(value["resource_keys"], list)
        or not 1 <= len(value["resource_keys"]) <= 100
        or not all(isinstance(k, str) for k in value["resource_keys"])
        or len(set(value["resource_keys"])) != len(value["resource_keys"])
        or "/devices/" + value["vehicle_resource_uuid"] not in value["resource_keys"]
    ):
        raise ValueError("车辆角色、资源包或代次无效")
    return value


def validate_event(value: Any) -> dict:
    fields = {
        "protocol_version",
        "event_uuid",
        "event_digest",
        "station_uuid",
        "instance_uuid",
        "event_epoch_uuid",
        "event_sequence",
        "request_uuid",
        "generation",
        "condition_digest",
        "fact",
    }
    if isinstance(value, dict) and "waiting_reason" in value:
        fields.add("waiting_reason")
        if value.get("fact") != "waiting" or value["waiting_reason"] not in {
            "route_unconfigured",
            "route_unavailable",
            "no_compatible_vehicle",
            "fleet_busy",
        }:
            raise ValueError("车辆准备等待原因无效")
    if isinstance(value, dict) and "binding" in value:
        fields.add("binding")
    if isinstance(value, dict) and "decision_uuid" in value:
        fields.add("decision_uuid")
    if not isinstance(value, dict) or set(value) != fields or value["protocol_version"] != "1.0":
        raise ValueError("准备事件信封无效")
    for key in ("event_uuid", "station_uuid", "instance_uuid", "event_epoch_uuid", "request_uuid"):
        uuid(value[key])
    for key in ("generation", "event_sequence"):
        if type(value[key]) is not int or not 1 <= value[key] <= 2**53 - 1:
            raise ValueError("准备事件序号或代次无效")
    if (
        value["fact"] not in {"waiting", "reserved", "closed", "released", "rejected"}
        or (value["fact"] == "reserved") != ("binding" in value)
        or (value["fact"] == "released") != ("decision_uuid" in value)
        or not is_digest(value["condition_digest"])
        or digest({k: v for k, v in value.items() if k != "event_digest"}) != value["event_digest"]
    ):
        raise ValueError("准备事件事实或摘要无效")
    if "decision_uuid" in value:
        uuid(value["decision_uuid"])
    if "binding" in value:
        b = validate_binding(value["binding"])
        if b["request_uuid"] != value["request_uuid"] or b["generation"] != value["generation"]:
            raise ValueError("准备结果代次与请求不符")
    return value
