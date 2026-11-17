"""导入边界的 SiteSlot 绑定；只改工作流参数，不创建或搬动物料。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID


class SiteBindingError(ValueError):
    """错误中保留节点、参数与匹配范围，调用方可直接展示。"""


def _dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


class SiteBindingIndex:
    def __init__(self, materials: Sequence[Any]):
        self.materials: dict[str, dict[str, Any]] = {}
        self.sites: dict[str, dict[str, Any]] = {}
        for aggregate in materials:
            record = _dict(aggregate)
            material = _dict(record.get("material", record))
            identity = str(material.get("material_uuid") or "")
            if not identity or material.get("deleted_at_ms") is not None:
                continue
            self.materials[identity] = material
            for raw_site in record.get("sites", []):
                site = _dict(raw_site)
                if site.get("deleted_at_ms") is None:
                    self.sites[str(site["site_uuid"])] = {**site, "owner_material_uuid": identity}

    def device(self, identity: str) -> dict[str, Any]:
        matches = [item for uuid, item in self.materials.items()
                   if uuid == identity or item.get("resource_id") == identity]
        return matches[0] if len(matches) == 1 else {}

    def contains(self, root: str, owner: str) -> bool:
        seen: set[str] = set()
        while owner and owner not in seen:
            if owner == root:
                return True
            seen.add(owner)
            owner = str(self.materials.get(owner, {}).get("parent_material_uuid") or "")
        return False

    def owner_ref(self, reference: Any) -> str:
        ref = _dict(reference)
        if ref.get("uuid"):
            identity = str(ref["uuid"])
            if identity in self.materials:
                return identity
            raise SiteBindingError(f"目标物料 UUID {identity!r} 不存在")
        label = str(ref.get("name") or ref.get("id") or "")
        # 名字引用按人读标签 / 业务 id 匹配（物料 name 是 uuid 时也照样命中）
        matches = [uuid for uuid, item in self.materials.items()
                   if label and label in (item.get("display_name"), item.get("name"), item.get("resource_id"))]
        if len(matches) != 1:
            raise SiteBindingError(f"目标物料 {label!r} 匹配到 {len(matches)} 项，请明确物料 UUID")
        return matches[0]

    def resolve(self, value: Any, *, scope: str, exact_owner: bool) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SiteBindingError("请绑定物料及 Site")
        value = value.strip()
        try:
            UUID(value)
        except ValueError:
            pass
        else:
            if value not in self.sites:
                raise SiteBindingError(f"Site UUID {value!r} 已失效，请重新绑定")
        if scope not in self.materials:
            raise SiteBindingError("目标物料/设备尚未登记，不能按全局同名 Site 自动绑定")
        candidates = [site for site in self.sites.values()
                      if (site["owner_material_uuid"] == scope if exact_owner
                          else self.contains(scope, site["owner_material_uuid"]))]
        # UUID 身份存在但不在作用域时，不能当作 label 再次匹配。
        if value in self.sites:
            matches = [site for site in candidates if site["site_uuid"] == value]
        else:
            matches = [site for site in candidates if site.get("label") == value]
        if len(matches) != 1:
            owner = self.materials[scope].get("display_name") or self.materials[scope].get("name", scope)
            raise SiteBindingError(
                f"Site {value!r} 在 {owner!r} 范围匹配到 {len(matches)} 项；"
                "请明确目标物料及 site_uuid（失效 UUID 不按标签回退）"
            )
        return str(matches[0]["site_uuid"])


def resolve_workflow_sites(
    nodes: Sequence[Any], *, registry: Any, materials: Sequence[Any],
    mapped_paths: Mapping[str, Sequence[str]] | None = None,
    endpoints: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """程序化导入自动解析；浏览器草稿保存必须显式跳过此步骤。

    mount_resource 是 Host 物料动作已定义的目标父级引用；普通设备动作默认只在
    该设备子树查找。跨设备/多个父级可用 meta_data.site_binding_owners[param]
    明确 owner material_uuid。禁止从任意 ResourceSlot 猜测目标（它可能是源物料）。
    """

    result = [deepcopy(_dict(node)) for node in nodes]
    index = SiteBindingIndex(materials)
    # 执行设备 ID 不等于物料树路径（工作站子设备尤其如此）。动作声明应与
    # 前端一样读权威 endpoint 能力；物料目录仅用于确定 Site 的实际归属。
    declarations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw_endpoint in endpoints:
        endpoint = _dict(raw_endpoint)
        if endpoint.get("state") != "online":
            continue
        for raw_capability in endpoint.get("action_capabilities", []):
            capability = _dict(raw_capability)
            if capability.get("state") != "active":
                continue
            key = (str(capability.get("device_uuid") or ""), str(capability.get("action_name") or ""))
            declarations.setdefault(key, []).append(_dict(capability.get("descriptor")))
    by_uuid = {str(node.get("uuid")): node for node in result}
    for node in result:
        ancestor = node
        seen: set[str] = set()
        disabled = False
        while ancestor and str(ancestor.get("uuid")) not in seen:
            seen.add(str(ancestor.get("uuid")))
            disabled |= bool(ancestor.get("disabled"))
            ancestor = by_uuid.get(str(ancestor.get("parent_uuid")), {})
        if disabled:
            continue
        if not node.get("action_name") or str(node.get("type") or "").lower() not in {
            "device_action", "ilab", "device", "action", "resource_action",
        }:
            continue
        meta = _dict(node.get("meta_data"))
        device_id = str(meta.get("target_device_id") or node.get("material_uuid") or "")
        device = index.device(device_id)
        klass = str(device.get("template_name") or "")
        if not klass and meta.get("target_device_id") == "host_node":
            klass = "host_node"
        label = node.get("name") or node.get("uuid") or node.get("action_name")
        action_name = str(node["action_name"])
        candidates = declarations.get((device_id, action_name)) or declarations.get((device_id, f"auto-{action_name}"))
        if candidates:
            if any(candidate != candidates[0] for candidate in candidates[1:]):
                raise SiteBindingError(f"节点 {label!r} 的动作声明在不同 endpoint 上不一致，请先对齐设备能力")
            declaration = candidates[0]
        else:
            declaration = registry.action_definition(klass, action_name) if registry is not None else None
        if declaration is None:
            raise SiteBindingError(
                f"节点 {label!r} 的目标设备或动作声明尚未就绪，无法校验 Site 参数；"
                "请补充登记，或用 site_binding_mode=preserve 保存待校验草稿"
            )
        action = _dict(declaration)
        goal = _dict(_dict(_dict(action.get("schema")).get("properties")).get("goal"))
        markers = {**_dict(goal.get("_unilabos_placeholder_info")), **_dict(action.get("placeholder_keys"))}
        param = deepcopy(_dict(node.get("param")))
        for field, marker in markers.items():
            if marker != "unilabos_sites":
                continue
            paths = (mapped_paths or {}).get(str(node.get("uuid")), ())
            if any(path == field or path.startswith(field + ".") or field.startswith(path + ".") for path in paths):
                continue
            value = param.get(field, _dict(action.get("goal_default")).get(field))
            if value in (None, "") and field not in goal.get("required", []):
                continue
            try:
                explicit = _dict(meta.get("site_binding_owners")).get(field)
                scope = str(device.get("material_uuid") or "")
                exact_owner = False
                if explicit:
                    scope, exact_owner = str(explicit), True
                elif param.get("mount_resource"):
                    scope, exact_owner = index.owner_ref(param["mount_resource"]), True
                param[field] = index.resolve(value, scope=scope, exact_owner=exact_owner)
                bindings = _dict(meta.get("site_bindings"))
                bindings[field] = {
                    "site_uuid": param[field],
                    "owner_material_uuid": index.sites[param[field]]["owner_material_uuid"],
                    "device_id": str(meta.get("target_device_id") or device.get("resource_id") or ""),
                    "action_name": str(node.get("action_name") or ""),
                }
                meta["site_bindings"] = bindings
            except SiteBindingError as error:
                label = node.get("name") or node.get("uuid") or node.get("action_name")
                raise SiteBindingError(f"节点 {label!r} 参数 {field!r}：{error}") from error
        node["param"] = param if node.get("param") is not None or param else None
        if meta:
            node["meta_data"] = meta
    return result
