"""将动作资源合同解析为权威物料、完整库位和托管范围。

``MaterialRole.move`` / ``pick`` / ``place`` 规范化成 ``transfer`` / ``transfer_step``：目标与来源按库位 uuid 给出时，
所属方（载架或设备根节点）由库位决定；库位参数带 ``MaterialRole.site_picker(of=...)`` 关系时，值也可以是所属方下的
库位名（工作流里写死的工位；库位 uuid 由物料权威建树时发号，不能写死）。搬运设备参数（``DeviceSlot``，设备 id）
解析成该设备的物料，省略时是执行设备。旧合同里显式的所属物料参数（ResourceSlot）照旧可用。
"""

from __future__ import annotations

from typing import Any, Mapping

from unilabos.protocol.material_execution import PhysicalMove, PhysicalAliquot
from unilabos.protocol.runtime.resource_intervals import DeviceTenancyTransition
from .resource_wait_policy import ResourceConditionWait


def site_owner_material_uuid(materials: Any, site_uuid: str) -> str | None:
    """库位 uuid → 所属物料 uuid（载架或设备根节点）；库位不存在（或已删除）时为 ``None``。"""

    reader = getattr(materials, "site_admission_facts", None)
    if not callable(reader) or not isinstance(site_uuid, str) or not site_uuid.strip():
        return None
    try:
        # 物料网关（嵌入 / HTTP）收一个请求体；物料服务本身收关键字参数
        result = reader({"site_uuids": [site_uuid]})
    except TypeError:
        result = reader(site_uuids=[site_uuid])
    facts = (result or {}).get("sites", {}).get(site_uuid) or {}
    if not facts.get("exists"):
        return None
    owner = facts.get("owner_material_uuid")
    return str(owner) if owner else None


def device_material_uuid(materials: Any, device_id: str) -> str:
    """设备 id（``DeviceSlot`` 的值）→ 该设备在物料权威里的物料 uuid；不是已登记设备时报错。"""

    aggregate = materials.get_material_by_resource_id(device_id)
    if aggregate.material.resource_type != "device" or aggregate.material.resource_id != device_id:
        raise ValueError(f"{device_id} 不是已登记的设备")
    return aggregate.material.material_uuid


def site_picker_owner(
    schema: Mapping[str, Any] | None, values: Mapping[str, Any], name: str, materials: Any, device_id: str | None,
) -> str | None:
    """库位参数 ``name`` 的 ``MaterialRole.site_picker`` 所属方 → 所属物料 uuid；没有这层关系或所属方未给时为 ``None``。

    ``of`` 是 ``ResourceSlot`` 时取它的物料 uuid，是 ``DeviceSlot`` 时取设备根节点，省略时是执行设备根节点。
    """

    fields = (((schema or {}).get("properties") or {}).get("goal") or {}).get("properties") or {}
    selector = (fields.get(name) or {}).get("x-unilabos-site-selector")
    if not isinstance(selector, Mapping):
        return None
    of = selector.get("owner")
    value = values.get(of) if of else device_id
    if isinstance(value, str) and value.strip():
        return device_material_uuid(materials, value)
    if isinstance(value, Mapping) and isinstance(value.get("uuid"), str) and value["uuid"].strip():
        return value["uuid"]
    return None


def site_by_label(
    schema: Mapping[str, Any] | None, values: Mapping[str, Any], name: str, materials: Any, device_id: str | None,
) -> tuple[str, str] | None:
    """库位参数的值不是已登记的库位 uuid 时（工作流里写死的库位名或序号）：在 ``MaterialRole.site_picker`` 的所属方下
    唯一匹配，返回 ``(所属物料 uuid, 库位 uuid)``；没有 site_picker 关系或匹配不唯一时为 ``None``。"""

    selected = values.get(name)
    if not isinstance(selected, str) or not selected.strip():
        return None
    owner = site_picker_owner(schema, values, name, materials, device_id)
    if not owner:
        return None
    matches = [
        item for item in materials.get_material(owner).sites
        if selected in (item.site_uuid, item.label, str(item.site_index))
    ]
    return (owner, matches[0].site_uuid) if len(matches) == 1 else None


def resolve_physical_contract(
    schema: Mapping[str, Any] | None, values: dict[str, Any], device_id: str, materials: Any
) -> dict[str, Any]:
    contract = ((schema or {}).get("x-unilabos-action-contract") or {}).get("resource_contract") or {}
    if not any(contract.get(key) for key in ("transfer", "transfer_step", "aliquot")):
        return {}
    if materials is None:
        raise ValueError("物理动作需要材料权威")
    ids, devices, reserved_sites = set(), set(), set()

    def resource(name):
        value = values.get(name)
        if not isinstance(value, dict) or not isinstance(value.get("uuid"), str):
            raise ValueError(f"物理参数 {name} 缺少 ResourceSlot 身份")
        return value["uuid"]

    def holder(name):
        """搬运设备 / 所属方参数：``DeviceSlot`` 的设备 id 解析成设备物料，旧合同的 ``ResourceSlot`` 照旧。"""

        value = values.get(name)
        if isinstance(value, str):
            if not value.strip():
                raise ValueError(f"设备参数 {name} 必须给出设备 id")
            try:
                return device_material_uuid(materials, value)
            except ValueError as error:
                raise ValueError(f"设备参数 {name}：{error}") from None
        return resource(name)

    def executor():
        return materials.get_material_by_resource_id(device_id).material.material_uuid

    def aggregate(identity):
        result = materials.get_material(identity)
        ids.add(identity)
        return result

    def owner_device(identity):
        seen = set()
        while identity and identity not in seen:
            seen.add(identity)
            item = aggregate(identity).material
            if item.resource_type == "device":
                devices.add(item.resource_id)
                return item.resource_id
            identity = item.parent_material_uuid
        return None

    def site(owner, selected):
        matches = [
            item
            for item in aggregate(owner).sites
            if item.site_uuid == selected or item.label == selected or str(item.site_index) == str(selected)
        ]
        if len(matches) != 1 or not matches[0].visible:
            raise ValueError("库位选择必须唯一且可见")
        return matches[0]

    def site_by_uuid(name):
        """库位参数（``SiteSlot``）→ ``(所属物料, 库位)``：库位 uuid 的所属方由库位决定；
        带 ``MaterialRole.site_picker`` 关系时也可以是所属方下的库位名。"""

        selected = values.get(name)
        if not isinstance(selected, str) or not selected.strip():
            raise ValueError(f"库位参数 {name} 必须给出库位 uuid")
        owner = site_owner_material_uuid(materials, selected)
        if owner is None:
            found = site_by_label(schema, values, name, materials, device_id)
            if found is None:
                raise ValueError(f"库位参数 {name} 指向不存在的库位 {selected}"
                                 "（不是库位 uuid，也不是 MaterialRole.site_picker 所属方下唯一的库位名）")
            owner, selected = found
        return owner, site(owner, selected)

    def location(identity):
        parent = aggregate(identity).material.parent_material_uuid
        if not parent:
            # 物料暂时不在任何库位（运输中、被取下）：等它回到库位，不算合同错误
            raise ResourceConditionWait("transfer_source_site_missing", "待搬运物料没有权威来源容器",
                                        material_uuid=identity)
        # 台面型 Site 的物料直接挂在 Site 所属物料下；载架型 Site（仓库等）的物料挂在
        # 该 Site 的 holder 下，Site 属于 holder 的父物料
        for owner in (parent, aggregate(parent).material.parent_material_uuid):
            if not owner:
                continue
            matches = [item for item in aggregate(owner).sites if item.occupied_material_uuid == identity]
            if len(matches) == 1:
                owner_device(owner)
                return owner, matches[0].site_uuid
            if matches:
                break
        raise ResourceConditionWait("transfer_source_site_missing", "待搬运物料没有唯一权威来源库位",
                                    material_uuid=identity)

    def gripper(owners, role, material):
        matches = [
            (owner, item) for owner in owners for item in aggregate(owner).sites if item.meta_data.get("role") == role
        ]
        if len(matches) != 1 or not matches[0][1].visible:
            raise ValueError(f"搬运设备必须有唯一可见的夹爪库位（meta_data.role={role}）")
        if matches[0][1].occupied_material_uuid not in (None, material):
            raise ResourceConditionWait("gripper_site_occupied", "搬运器的夹爪库位正被其他物料占用")
        reserved_sites.add(matches[0][1].site_uuid)
        for owner in owners:
            owner_device(owner)
        return matches[0]

    for parameter in contract.get("resource_params", []):
        if parameter["role"] in {"device", "motion", "tool", "material"}:
            value = values.get(parameter["param"])
            identity = (
                materials.get_material_by_resource_id(value).material.material_uuid
                if isinstance(value, str)
                else resource(parameter["param"])
            )
            aggregate(identity)
            owner_device(identity)
    moves, aliquot, tenancy = [], None, None
    transfer = contract.get("transfer")
    if transfer:
        identity = resource(transfer["material_param"])
        source_owner, source_site = location(identity)
        ids.update(item.material.material_uuid for item in materials.get_tree(identity).nodes)
        if transfer.get("target_owner_param"):
            target_owner = resource(transfer["target_owner_param"])
            target = site(
                target_owner,
                values.get(transfer.get("target_site_uuid_param")) or values.get(transfer.get("target_site_name_param")),
            )
        else:
            target_owner, target = site_by_uuid(transfer["target_site_uuid_param"])
        requested = None
        if transfer.get("source_owner_param"):
            requested_owner = resource(transfer["source_owner_param"])
            requested = (requested_owner, site(
                requested_owner,
                values.get(transfer.get("source_site_uuid_param"))
                or values.get(transfer.get("source_site_name_param")),
            ).site_uuid)
        elif transfer.get("source_site_uuid_param") and values.get(transfer["source_site_uuid_param"]):
            requested_owner, requested_site = site_by_uuid(transfer["source_site_uuid_param"])
            requested = (requested_owner, requested_site.site_uuid)
        if requested is not None and requested != (source_owner, source_site):
            raise ResourceConditionWait("transfer_source_site_missing", "声明的搬运来源与权威库位不一致",
                                        material_uuid=identity)
        carrier = holder(transfer["carrier_device_param"]) if transfer.get("carrier_device_param") else executor()
        gripper([carrier], transfer["gripper_site_role"], identity)
        owner_device(target_owner)
        moves.append(
            PhysicalMove(
                material_uuid=identity,
                source_owner_uuid=source_owner,
                source_site_uuid=source_site,
                target_owner_uuid=target_owner,
                target_site_uuid=target.site_uuid,
            )
        )
    step = contract.get("transfer_step")
    if step:
        if transfer:
            raise ValueError("一次动作不能同时声明 transfer 与 transfer_step")
        identity = resource(step["material_param"])
        source_owner, source_site = location(identity)
        ids.update(item.material.material_uuid for item in materials.get_tree(identity).nodes)
        end_owner, end_site = None, None
        if step.get("owner_param"):
            end_owner = resource(step["owner_param"])
            end_site = site(end_owner, values.get(step["site_param"]))
        elif step.get("site_param") and values.get(step["site_param"]):
            end_owner, end_site = site_by_uuid(step["site_param"])
        elif step["operation"] == "place":
            raise ValueError(f"放置动作缺少目标库位参数 {step.get('site_param') or ''}")
        carriers = [holder(name) for name in step["carrier_params"]] or [executor()]
        carrier, grip = gripper(carriers, "gripper", identity)
        carrier_device = owner_device(carrier)
        if not carrier_device:
            raise ValueError("分步搬运的夹爪必须归属于设备，才能跨动作托管")
        if step["operation"] == "pick":
            # from_site 省略时从权威当前位置抓取；给出时必须与权威一致
            if end_owner is not None and (source_owner, source_site) != (end_owner, end_site.site_uuid):
                raise ResourceConditionWait("transfer_source_site_missing", "抓取来源与权威库位不一致",
                                            material_uuid=identity)
            target_owner, target_site = carrier, grip.site_uuid
            tenancy = DeviceTenancyTransition(material_uuid=identity, acquire_device_id=carrier_device)
        else:
            if (source_owner, source_site) != (carrier, grip.site_uuid):
                raise ValueError("放置前样品必须仍托管在指定夹爪")
            target_owner, target_site = end_owner, end_site.site_uuid
            tenancy = DeviceTenancyTransition(material_uuid=identity, release_device_id=carrier_device)
        if end_owner is not None:
            owner_device(end_owner)
        moves.append(
            PhysicalMove(
                material_uuid=identity,
                source_owner_uuid=source_owner,
                source_site_uuid=source_site,
                target_owner_uuid=target_owner,
                target_site_uuid=target_site,
            )
        )
    if contract.get("aliquot"):
        declaration = contract["aliquot"]
        source = resource(declaration["source_material_param"])
        targets = [resource(name) for name in declaration["target_material_params"]]
        aliquot = PhysicalAliquot(source_material_uuid=source, target_material_uuids=targets)
        for identity in [source, *targets]:
            aggregate(identity)
            owner_device(identity)
    return {
        "moves": moves,
        "aliquot": aliquot,
        "material_ids": sorted(ids),
        "device_ids": sorted(devices),
        "reserved_site_uuids": sorted(reserved_sites),
        "device_tenancy": tenancy,
    }
