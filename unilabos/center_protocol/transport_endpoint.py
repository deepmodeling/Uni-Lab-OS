"""交接位置快照的严格协议校验，不从工站编号推测实际库位。"""

from typing import Any
from uuid import UUID


def validate_endpoint(value: Any) -> dict[str, Any]:
    """校验一个工站的整批真实容器位置及库存版本。"""
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "station_uuid", "handoff_revision", "items"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["handoff_revision"]) is not int
        or not 1 <= value["handoff_revision"] <= 2**53 - 1
        or not isinstance(value["items"], list)
        or not 1 <= len(value["items"]) <= 100
    ):
        raise ValueError("交接位置快照格式无效")

    def identity(v):
        if not isinstance(v, str) or str(UUID(v)) != v or UUID(v).int == 0:
            raise ValueError("交接位置必须使用规范 UUID")

    identity(value["station_uuid"])
    containers = set()
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) - {"reservation"} != {
            "container_uuid",
            "material_uuid",
            "site_uuid",
            "material_revision",
            "site_digest",
            "site_label",
        }:
            raise ValueError("交接位置明细字段无效")
        for key in ("container_uuid", "material_uuid", "site_uuid"):
            identity(item[key])
        if (
            item["container_uuid"] in containers
            or type(item["material_revision"]) is not int
            or not 1 <= item["material_revision"] <= 2**53 - 1
        ):
            raise ValueError("交接位置容器重复或版本无效")
        from .digest import is_digest

        if (
            not isinstance(item["site_label"], str)
            or not item["site_label"].strip()
            or len(item["site_label"].encode()) > 500
        ):
            raise ValueError("交接位置缺少可核对的实际名称")
        if not is_digest(item["site_digest"]):
            raise ValueError("交接位置事实摘要无效")
        if "reservation" in item:
            r = item["reservation"]
            if (
                not isinstance(r, dict)
                or set(r) != {"reservation_uuid", "revision", "valid_until_ms"}
                or type(r["revision"]) is not int
                or not 1 <= r["revision"] <= 2**53 - 1
                or type(r["valid_until_ms"]) is not int
                or not 1 <= r["valid_until_ms"] <= 2**53 - 1
            ):
                raise ValueError("接收保护身份、版本或期限无效")
            identity(r["reservation_uuid"])
        containers.add(item["container_uuid"])
    return value
