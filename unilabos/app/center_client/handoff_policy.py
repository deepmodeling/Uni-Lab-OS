"""从显式安装配置生成本站接收方案，不猜测远端模板或真实位置。"""

from __future__ import annotations

import json
from pathlib import Path

from unilabos.center_protocol.transport_preparation import uuid


class InstalledHandoffPolicy:
    def __init__(self, path: str):
        self.value = json.loads(Path(path).read_text())
        allowed = {
            "schema_version",
            "carrier_template_uuid",
            "template_map",
            "candidate_site_uuids",
            "ttl_seconds",
            "site_names",
            "reagent_info_uuids",
            "adapter_provider",
        }
        if (
            not isinstance(self.value, dict)
            or set(self.value) - allowed
            or type(self.value.get("schema_version")) is not int
            or self.value.get("schema_version") != 1
            or not isinstance(self.value.get("template_map", {}), dict)
            or not isinstance(self.value.get("candidate_site_uuids"), list)
            or not self.value["candidate_site_uuids"]
            or type(self.value.get("ttl_seconds")) is not int
            or not 60 <= self.value["ttl_seconds"] <= 86400
        ):
            raise ValueError("交接安装策略字段或接收期限无效")
        uuid(self.value["carrier_template_uuid"])
        for identity in [
            *self.value["candidate_site_uuids"],
            *self.value.get("template_map", {}).keys(),
            *self.value.get("template_map", {}).values(),
        ]:
            uuid(identity)

    def plans(self, payload):
        policy, result = self.value, {}
        for item in payload["items"]:
            snapshot = item["source_snapshot"]
            materials = {m["local_material_uuid"]: m for m in snapshot.get("materials", [])}
            carrier = snapshot.get("carrier_material_uuid")
            if carrier is None and len(item["source_material_refs"]) == 1:
                carrier = item["source_material_refs"][0]["local_material_uuid"]
            mapped, carrier_lineage = {}, None
            for ref in item["source_material_refs"]:
                local, lineage = ref["local_material_uuid"], ref["material_lineage_uuid"]
                if local == carrier:
                    mapped[lineage], carrier_lineage = policy["carrier_template_uuid"], lineage
                else:
                    source_template = materials.get(local, {}).get("resource_template_uuid")
                    if source_template not in policy.get("template_map", {}):
                        raise ValueError("本站没有该来源物料的显式模板映射")
                    mapped[lineage] = policy["template_map"][source_template]
            plan = {
                "carrier_template_uuid": policy["carrier_template_uuid"],
                "carrier_lineage_uuid": carrier_lineage,
                "lineage_template_uuids": mapped,
                "candidate_site_uuids": policy["candidate_site_uuids"],
                "ttl_seconds": policy["ttl_seconds"],
            }
            if snapshot.get("adapter") == "unilab.inventory.v1":
                names = {}
                for local, material in materials.items():
                    configured = policy.get("site_names", {}).get(material.get("resource_template_uuid"), {})
                    if material.get("sites") and not configured:
                        raise ValueError("复杂容器缺少本站显式内部库位映射")
                    names[local] = configured
                plan["adapter"] = {
                    "kind": "unilab.inventory.v1",
                    "site_names": names,
                    "reagent_info_uuids": policy.get("reagent_info_uuids", {}),
                }
            result[item["handoff_item_uuid"]] = plan
        return result
