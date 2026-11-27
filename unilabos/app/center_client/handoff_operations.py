"""中心交接意图的站端执行器；身份、物理核对和原子库存事实缺一不可。"""

from __future__ import annotations

import json
from typing import Any, Callable

from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.transport_preparation import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS station_handoff_operation (
 handoff_uuid TEXT NOT NULL, operation_uuid TEXT NOT NULL, kind TEXT NOT NULL, request_digest TEXT NOT NULL,
 request_json TEXT NOT NULL, parameters_json TEXT, result_json TEXT,
 PRIMARY KEY(handoff_uuid,operation_uuid,kind)
);
"""


class HandoffOperations:
    """准备方案与真实核对先固定；崩溃后重放原库存操作及原证据。"""

    def __init__(self, bridge: Any, *, source_ready: Callable | None = None, arrival: Callable | None = None):
        self.bridge, self.source_ready, self.arrival = bridge, source_ready, arrival
        with bridge.authority.store.transaction() as c:
            c.executescript(SCHEMA)

    def handle(self, command: dict) -> bool:
        kind, request = command["type"], command["payload"]
        if kind not in {
            "handoff.target_prepare",
            "handoff.source_release",
            "handoff.target_receive",
            "handoff.target_renew",
        }:
            return False
        expected = {
            "schema_version",
            "operation_uuid",
            "handoff_uuid",
            "expected_revision",
            "station_uuid",
            "event_epoch_uuid",
            "actor_uuid",
            "details",
            "payload_digest",
        }
        if (
            not isinstance(request, dict)
            or set(request) != expected
            or type(request["schema_version"]) is not int
            or request["schema_version"] != 1
            or type(request["expected_revision"]) is not int
            or not 1 <= request["expected_revision"] <= 2**53 - 1
            or not isinstance(request["details"], dict)
            or digest({k: v for k, v in request.items() if k != "payload_digest"}) != request["payload_digest"]
        ):
            raise ValueError("交接操作信封或摘要无效")
        for key in ("operation_uuid", "handoff_uuid", "station_uuid", "event_epoch_uuid"):
            uuid(request[key])
        if request["actor_uuid"] is not None:
            uuid(request["actor_uuid"])
        bridge, authority = self.bridge, self.bridge.authority
        if (request["station_uuid"], request["event_epoch_uuid"]) != (
            authority.station_uuid,
            authority.event_epoch_uuid,
        ):
            raise ValueError("交接操作不属于本站当前事件纪元")
        store = authority.store
        prior = store.query_one(
            "SELECT * FROM station_handoff_operation WHERE handoff_uuid=? AND operation_uuid=? AND kind=?",
            (request["handoff_uuid"], request["operation_uuid"], kind),
        )
        if prior is not None:
            if prior["request_digest"] != request["payload_digest"]:
                raise ValueError("原交接操作身份被不同确认内容重复使用")
            if prior["result_json"] is not None:
                return True
        package = bridge.client.handoff_package(request["handoff_uuid"], None, authority.event_epoch_uuid)
        payload = authority._package(package, "source" if kind == "handoff.source_release" else "target")
        if payload["revision"] < request["expected_revision"]:
            raise ValueError("交接操作尚未取得期望修订")
        if payload["attention"] or payload["status"] in {"canceling", "canceled"}:
            # 仍应用取消，阻止旧命令恢复保护；不能把它当作原业务操作成功。
            authority.apply_center_snapshot(package)
            raise ValueError("交接已经取消或需要核实，禁止旧操作复活")
        params = (
            json.loads(prior["parameters_json"])
            if prior and prior["parameters_json"]
            else self._parameters(kind, request, package)
        )
        if kind == "handoff.target_receive" and request["actor_uuid"] is None and prior is not None:
            fresh = self._parameters(kind, request, package)
            if fresh:
                known = {item["handoff_item_uuid"] for item in params["items"]}
                params = {
                    "items": params["items"]
                    + [item for item in fresh["items"] if item["handoff_item_uuid"] not in known]
                }
        if params is None:
            return False
        with store.transaction() as c:
            c.execute(
                "INSERT INTO station_handoff_operation VALUES(?,?,?,?,?,?,NULL) ON CONFLICT DO NOTHING",
                (
                    request["handoff_uuid"],
                    request["operation_uuid"],
                    kind,
                    request["payload_digest"],
                    json.dumps(request),
                    json.dumps(params),
                ),
            )
            row = c.execute(
                "SELECT * FROM station_handoff_operation WHERE handoff_uuid=? AND operation_uuid=? AND kind=?",
                (request["handoff_uuid"], request["operation_uuid"], kind),
            ).fetchone()
            if row["request_digest"] != request["payload_digest"]:
                raise ValueError("原交接操作并发内容冲突")
            if kind == "handoff.target_receive" and request["actor_uuid"] is None:
                # 只追加首次观察到的容器证据；已成功条目的证据保持原样。
                old_params = json.loads(row["parameters_json"])
                known = {i["handoff_item_uuid"] for i in old_params["items"]}
                params = {
                    "items": old_params["items"] + [i for i in params["items"] if i["handoff_item_uuid"] not in known]
                }
                c.execute(
                    "UPDATE station_handoff_operation SET parameters_json=? WHERE handoff_uuid=? AND operation_uuid=? AND kind=?",
                    (json.dumps(params), request["handoff_uuid"], request["operation_uuid"], kind),
                )
            else:
                params = json.loads(row["parameters_json"])
        if kind == "handoff.target_prepare":
            details = {
                i["container_uuid"]: bridge.client.container(
                    i["container_uuid"], authority.event_epoch_uuid, handoff_uuid=payload["handoff_uuid"]
                )
                for i in payload["items"]
            }
            result = authority.prepare_incoming(
                package, container_details=details, plans=params["plans"], include_transport_endpoints=True
            )
        elif kind == "handoff.source_release":
            result = authority.freeze_source(
                package, physical_ready=True, evidence_refs=params["evidence_refs"], include_transport_endpoints=True
            )
        elif kind == "handoff.target_receive":
            authority.apply_center_snapshot(package)
            result = []
            for item in params["items"]:
                # 核对实际落位与原保护位置，禁止把页面里任意库位当作收货事实。
                reservation = store.query_one(
                    "SELECT r.site_uuid FROM station_handoff_item i JOIN station_ingress_reservation r "
                    "ON r.uuid=i.reservation_uuid WHERE i.handoff_uuid=? AND i.handoff_item_uuid=?",
                    (payload["handoff_uuid"], item["handoff_item_uuid"]),
                )
                if reservation is None or reservation["site_uuid"] != item["actual_site_uuid"]:
                    raise ValueError("实际落位与原接收保护位置不一致，需要核实")
                result.append(
                    authority.receive(
                        payload["handoff_uuid"],
                        item["handoff_item_uuid"],
                        barcode_namespace=item["barcode_namespace"],
                        barcode=item["barcode"],
                        evidence_refs=item["evidence_refs"],
                    )
                )
        else:
            result = authority.renew_incoming(package, ttl_seconds=params["ttl_seconds"])
        if kind == "handoff.target_receive" and request["actor_uuid"] is None:
            received = {
                row["handoff_item_uuid"]
                for row in store.query_all(
                    "SELECT handoff_item_uuid FROM station_handoff_item WHERE handoff_uuid=? AND receipt_json IS NOT NULL",
                    (payload["handoff_uuid"],),
                )
            }
            if not all(item["handoff_item_uuid"] in received for item in payload["items"]):
                return False
        with store.transaction() as c:
            c.execute(
                "UPDATE station_handoff_operation SET result_json=? WHERE handoff_uuid=? AND operation_uuid=? AND kind=?",
                (json.dumps(result), request["handoff_uuid"], request["operation_uuid"], kind),
            )
        return True

    def _parameters(self, kind, request, package):
        details, bridge = request["details"], self.bridge
        marker = "manual-operation://" + request["operation_uuid"]
        if kind == "handoff.target_prepare":
            plans = bridge.incoming_plans(package["payload"]) if bridge.incoming_plans else None
            return {"plans": plans} if plans is not None else None
        if kind == "handoff.source_release":
            if request["actor_uuid"] is not None:
                verification = _verification(details.get("verification"))
                if details.get("physical_ready") is not True:
                    raise ValueError("人工放行缺少明确物理确认")
                for item in package["payload"]["items"]:
                    bound = bridge.authority.store.query_one(
                        "SELECT barcode FROM container_station_binding WHERE container_uuid=?",
                        (item["container_uuid"],),
                    )
                    if bound is None or bound["barcode"] not in verification["observed_identifiers"]:
                        raise ValueError("源实物标识未经本次人工核对")
                return {
                    "evidence_refs": [
                        marker,
                        f"{verification['method']}://{request['actor_uuid']}/{digest(verification)}",
                    ]
                }
            proof = self.source_ready(package) if self.source_ready else None
            if not proof or proof.get("physical_ready") is not True or not proof.get("evidence_refs"):
                return None
            return {"evidence_refs": proof["evidence_refs"]}
        if kind == "handoff.target_receive":
            if request["actor_uuid"] is None:
                if details.get("require_pickup") is True and package["payload"].get("pickup") is None:
                    return None
                proof = self.arrival(package) if self.arrival else None
                return {"items": proof} if proof else None
            items = []
            if (
                set(details) != {"items"}
                or not isinstance(details["items"], list)
                or not 1 <= len(details["items"]) <= 100
            ):
                raise ValueError("人工实收明细无效")
            for item in details["items"]:
                if set(item) != {
                    "handoff_item_uuid",
                    "barcode_namespace",
                    "barcode",
                    "actual_site_uuid",
                    "verification",
                }:
                    raise ValueError("人工实收字段无效")
                uuid(item["handoff_item_uuid"])
                uuid(item["actual_site_uuid"])
                verification = _verification(item["verification"])
                if item["barcode"] not in verification["observed_identifiers"]:
                    raise ValueError("实收标识未经本次人工核对")
                items.append(
                    {
                        **{k: v for k, v in item.items() if k != "verification"},
                        "evidence_refs": [
                            marker,
                            f"{verification['method']}://{request['actor_uuid']}/{digest(verification)}",
                        ],
                    }
                )
            return {"items": items}
        if (
            set(details) != {"ttl_seconds"}
            or type(details["ttl_seconds"]) is not int
            or not 1 <= details["ttl_seconds"] <= 86400
        ):
            raise ValueError("接收续期参数无效")
        return details


def _verification(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"method", "observed_identifiers", "evidence_refs"}
        or value["method"] not in {"manual_visual", "barcode_scan", "station_sensor"}
        or not all(
            isinstance(value[k], list)
            and 1 <= len(value[k]) <= 100
            and all(isinstance(v, str) and v.strip() and len(v) <= 1000 for v in value[k])
            for k in ("observed_identifiers", "evidence_refs")
        )
    ):
        raise ValueError("缺少真实实物核对证据")
    return value
