"""物流站安装的接驳映射；导航参数只从已审阅配置解析并固定摘要。"""

from __future__ import annotations

import json
from pathlib import Path

from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.transport_preparation import uuid


class TransportRoutes:
    def __init__(self, path: str):
        self.path = Path(path)
        self.profile = self._read()
        self.profile_digest = digest(self.profile)

    def _read(self):
        value = json.loads(self.path.read_text())
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "profile_uuid", "sites"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not isinstance(value["sites"], list)
            or not 1 <= len(value["sites"]) <= 1000
        ):
            raise ValueError("物流接驳安装配置无效")
        uuid(value["profile_uuid"])
        seen = set()
        for site in value["sites"]:
            if (
                not isinstance(site, dict)
                or set(site) != {"station_uuid", "site_uuid", "nav_point_ref", "slot_ref"}
                or any(
                    not isinstance(site[k], str) or not site[k].strip() or len(site[k]) > 200
                    for k in ("nav_point_ref", "slot_ref")
                )
            ):
                raise ValueError("物流接驳映射缺少明确导航点或槽位")
            uuid(site["station_uuid"])
            uuid(site["site_uuid"])
            identity = (site["station_uuid"], site["site_uuid"])
            if identity in seen:
                raise ValueError("物流接驳映射存在重复身份")
            seen.add(identity)
        return value

    def installed(self) -> bool:
        """公开目录只报告当前已安装且摘要一致的接驳能力。"""
        try:
            return digest(self._read()) == self.profile_digest
        except (OSError, ValueError):
            return False

    def resolve(self, request):
        if digest(self._read()) != self.profile_digest:
            raise ValueError("物流接驳配置发生变化，必须重新安装并核实原车辆占用")
        index = {(s["station_uuid"], s["site_uuid"]): s for s in self.profile["sites"]}
        binding = {
            "schema_version": 1,
            "profile_uuid": self.profile["profile_uuid"],
            "profile_digest": self.profile_digest,
        }
        for side in ("source", "target"):
            endpoint = request[side + "_endpoint"]
            items = []
            for item in endpoint["items"]:
                site = index.get((endpoint["station_uuid"], item["site_uuid"]))
                if site is None:
                    raise ValueError("真实取送货位置没有已安装接驳映射")
                items.append(
                    {
                        "container_uuid": item["container_uuid"],
                        "site_uuid": item["site_uuid"],
                        "nav_point_ref": site["nav_point_ref"],
                        "slot_ref": site["slot_ref"],
                    }
                )
            binding[side] = items
        return binding

    def verify(self, binding):
        route = binding.get("route_binding")
        if (
            route is None
            or route.get("profile_digest") != self.profile_digest
            or digest(self._read()) != self.profile_digest
            or digest(route) != binding.get("route_binding_digest")
        ):
            raise ValueError("最终任务包的路线绑定与当前安装配置不一致")
