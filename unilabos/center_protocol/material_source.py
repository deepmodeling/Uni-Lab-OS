"""中心静态来源绑定的闭合协议；不会补造公开输入端口或物料谱系。"""
from copy import deepcopy
from typing import Any
from uuid import UUID

from unilabos.center_protocol.digest import canonical_json, is_digest


def _uuid(value: Any) -> None:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("静态来源身份必须为规范 UUID")


def _revision(value: Any) -> None:
    if type(value) is not int or not 1 <= value <= 2**53 - 1:
        raise ValueError("静态来源修订无效")


def validate_material_source_bindings(values: Any, *, station_uuid: str) -> list[dict[str, Any]]:
    """验证完整字段及同容器引用覆盖，具体选择器和库存事实仍由本站原子核验。"""
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("静态来源绑定须为 1 至 100 项")
    nodes, materials, covered = set(), set(), set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {"node_uuid", "selector_digest", "material_uuid",
                "resource_template_uuid", "local_revision", "inventory_snapshot", "lineage"}:
            raise ValueError("静态来源绑定字段缺失或包含未知字段")
        for key in ("node_uuid", "material_uuid", "resource_template_uuid"):
            _uuid(item[key])
        _revision(item["local_revision"])
        if item["node_uuid"] in nodes or item["material_uuid"] in materials or not is_digest(item["selector_digest"]):
            raise ValueError("静态来源节点或独占物料重复，或选择器摘要无效")
        nodes.add(item["node_uuid"])
        materials.add(item["material_uuid"])
        snapshot = item["inventory_snapshot"]
        if (not isinstance(snapshot, dict) or set(snapshot) != {"adapter", "carrier_material_uuid", "materials"}
                or snapshot["adapter"] != "unilab.inventory.v1" or snapshot["carrier_material_uuid"] != item["material_uuid"]
                or not isinstance(snapshot["materials"], list) or not 1 <= len(snapshot["materials"]) <= 100):
            raise ValueError("静态来源必须包含完整载体库存快照")
        current = {}
        for material in snapshot["materials"]:
            if not isinstance(material, dict) or set(material) - {"name"} != {"local_material_uuid", "local_revision",
                    "parent_material_uuid", "barcode", "sites", "content", "capacity", "resource_template_uuid"}:
                raise ValueError("静态来源库存快照字段无效")
            if "name" in material and (not isinstance(material["name"], str) or not material["name"].strip()):
                raise ValueError("静态来源物料名称必须为非空字符串")
            _uuid(material["local_material_uuid"])
            _uuid(material["resource_template_uuid"])
            _revision(material["local_revision"])
            if material["local_material_uuid"] in current:
                raise ValueError("静态来源快照包含重复物料")
            current[material["local_material_uuid"]] = material
        if covered & set(current):
            raise ValueError("静态独占来源的载体或内容重叠")
        covered.update(current)
        root = current.get(item["material_uuid"])
        if root is None or root["local_revision"] != item["local_revision"] or root["resource_template_uuid"] != item["resource_template_uuid"]:
            raise ValueError("静态来源载体与快照身份、修订或模板不符")
        lineage = item["lineage"]
        if lineage == {"kind": "unseen"}:
            continue
        if not isinstance(lineage, dict) or set(lineage) != {"kind", "container_uuid", "container_state_version", "material_refs"} or lineage["kind"] != "retained":
            raise ValueError("静态来源谱系类型无效")
        _uuid(lineage["container_uuid"])
        _revision(lineage["container_state_version"])
        from unilabos.center_protocol.handoff import _refs
        _refs(lineage["material_refs"], station_uuid)
        if {ref["local_material_uuid"] for ref in lineage["material_refs"]} != set(current):
            raise ValueError("静态来源引用未精确覆盖完整库存树")
        if any(ref["local_revision"] != current[ref["local_material_uuid"]]["local_revision"] for ref in lineage["material_refs"]):
            raise ValueError("静态来源引用与快照修订不一致")
    if len(canonical_json(values)) > 1024 * 1024:
        raise ValueError("静态来源证明超过大小限制")
    return deepcopy(values)


def requires_material_input(payload: dict[str, Any]) -> bool:
    """首次新来源同样需要稳定任务身份及持久库存输入占用。"""
    return any(payload.get(name) for name in ("handoff_uuids", "material_refs", "container_refs", "material_source_bindings"))
